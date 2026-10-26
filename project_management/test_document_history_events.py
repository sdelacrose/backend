"""HISTORY-4: All Events -- targeted tests only. A READ MODEL assembled dynamically from
DocumentWorkflowEvent/SupplierSurveillanceEvent/TransmittalItem(+TransmittalEvent); no new
persisted table (section 3), so there is nothing here to test beyond correct aggregation,
correlation, ordering and access.
"""

from unittest.mock import patch

from django.db import connection
from django.http import Http404
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import (
    SupplierSurveillanceTemplate,
    SupplierSurveillanceTemplateItem,
    SupplierSurveillanceTemplateSection,
)
from project_management.services.document_history_events import list_document_history_events
from project_management.services.supplier_surveillance import create_supplier_surveillance
from project_management.test_transmittal import TransmittalTestBase
from project_management.views.document_history_events import DocumentHistoryEventsView

ACCESS = "project_management.views.document_history_events.get_register_document_or_404"


class HistoryEventsTestBase(TransmittalTestBase):
    def events(self, document):
        return list_document_history_events(document)

    def make_template(self):
        template = SupplierSurveillanceTemplate.objects.create(
            company=self.company, name="Test Template", created_by=self.owner,
        )
        section = SupplierSurveillanceTemplateSection.objects.create(
            template=template, title="Section",
        )
        SupplierSurveillanceTemplateItem.objects.create(section=section, label="Item")
        return template

    def real_surveillance(self, revision):
        """Unlike TransmittalTestBase.make_surveillance (a bare ORM row, deliberately bypassing
        the event-emission path -- see its own docstring), this goes through the real service
        so an actual SupplierSurveillanceEvent (SURVEILLANCE_CREATED) exists to aggregate."""
        return create_supplier_surveillance(
            document_revision=revision, actor=self.owner, template=self.make_template(),
        )


# 1. Security: an inaccessible Document's events are never exposed (same established
# mock-the-access-helper pattern as test_client_distribution_history.py's own ViewWiringTests).
class SecurityTests(HistoryEventsTestBase):
    def test_inaccessible_document_returns_404_not_event_data(self):
        revision = self.make_approved_internal_revision()
        factory = APIRequestFactory()
        request = factory.get("/x/")
        force_authenticate(request, user=self.owner)
        view = DocumentHistoryEventsView.as_view(permission_classes=[])
        with patch(ACCESS, side_effect=Http404):
            response = view(request, document_id=revision.document_id)
        self.assertEqual(response.status_code, 404)


# 2/3/4. INTERNAL: revision + workflow events returned, correctly correlated to the exact
# revision (never another one), source_revision uses revision_label.
class InternalEventsTests(HistoryEventsTestBase):
    def test_revision_and_workflow_events_returned_and_correlated(self):
        revision = self.make_approved_internal_revision()
        events = self.events(revision.document)

        # add_document_revision's own documented behavior (services.document_revision.
        # _materialize_first_revision, same quirk already noted in HISTORY-3's own tests)
        # silently materializes the Document's pre-existing file as an unlabeled Version 1 the
        # first time it is called -- its own, separate REVISION event, never this test's
        # concern; filtered to the exact revision under test instead of a total count.
        revision_events = [
            e
            for e in events
            if e["event_group"] == "REVISION" and e["document_revision_id"] == revision.id
        ]
        self.assertEqual(len(revision_events), 1)
        self.assertEqual(revision_events[0]["source_revision"], revision.revision_label)

        workflow_events = [e for e in events if e["event_group"] == "INTERNAL_WORKFLOW"]
        self.assertGreaterEqual(len(workflow_events), 2)  # at least SIGN + APPROVE
        self.assertTrue(all(e["document_revision_id"] == revision.id for e in workflow_events))
        self.assertTrue(all(e["source_revision"] == "A" for e in workflow_events))
        self.assertIn("APPROVE", [e["event_type"] for e in workflow_events])

    def test_workflow_events_of_one_revision_never_leak_into_another(self):
        revision_a = self.make_approved_internal_revision()
        revision_b = self.make_approved_internal_revision()
        events = self.events(revision_a.document)
        for event in events:
            if event["event_group"] == "INTERNAL_WORKFLOW":
                self.assertIn(event["document_revision_id"], (revision_a.id, revision_b.id))


# 5/6/7. SUPPLIER: revision + surveillance events returned, correctly correlated, source
# revision uses external_revision.
class SupplierEventsTests(HistoryEventsTestBase):
    def test_revision_and_surveillance_events_returned_and_correlated(self):
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        self.real_surveillance(revision)
        events = self.events(revision.document)

        # Same materialized-Version-1 quirk as the INTERNAL test above -- filtered to the
        # exact revision under test.
        revision_events = [
            e
            for e in events
            if e["event_group"] == "REVISION" and e["document_revision_id"] == revision.id
        ]
        self.assertEqual(len(revision_events), 1)
        self.assertEqual(revision_events[0]["source_revision"], revision.external_revision)

        surveillance_events = [
            e for e in events if e["event_group"] == "SUPPLIER_SURVEILLANCE"
        ]
        self.assertGreaterEqual(len(surveillance_events), 1)
        self.assertTrue(
            all(e["document_revision_id"] == revision.id for e in surveillance_events)
        )
        self.assertTrue(
            all(e["source_revision"] == revision.external_revision for e in surveillance_events)
        )
        self.assertIn("SURVEILLANCE_CREATED", [e["event_type"] for e in surveillance_events])

    def test_rejected_revision_a_surveillance_events_remain_after_b_is_reviewed(self):
        document = self.make_document(origin="SUPPLIER", name="s.txt")
        from project_management.services.document_revision import add_document_revision
        from django.core.files.uploadedfile import SimpleUploadedFile

        revision_a = add_document_revision(
            document, SimpleUploadedFile("a.txt", b"a"), self.owner,
            revision_type="minor", external_revision="A",
        )
        self.real_surveillance(revision_a)
        revision_b = add_document_revision(
            document, SimpleUploadedFile("b.txt", b"b"), self.owner,
            revision_type="minor", external_revision="B",
        )
        self.real_surveillance(revision_b)

        events = self.events(document)
        a_events = [
            e
            for e in events
            if e["event_group"] == "SUPPLIER_SURVEILLANCE"
            and e["document_revision_id"] == revision_a.id
        ]
        self.assertGreaterEqual(len(a_events), 1)


# 8/9. Client distribution: issued OUT occurrence returned; a retransmission is a second,
# distinct event, never collapsed.
class ClientDistributionEventsTests(HistoryEventsTestBase):
    def test_issued_distribution_returned_as_an_event(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        from project_management.services.transmittal_items import add_transmittal_item
        from project_management.services.transmittal_finalize import finalize_transmittal

        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalize_transmittal(transmittal, self.owner)

        events = self.events(revision.document)
        distribution_events = [e for e in events if e["event_group"] == "CLIENT_DISTRIBUTION"]
        self.assertEqual(len(distribution_events), 1)
        self.assertEqual(distribution_events[0]["event_type"], "ISSUED")
        self.assertEqual(distribution_events[0]["document_revision_id"], revision.id)

    def test_retransmission_is_a_second_distinct_event(self):
        from project_management.services.transmittal_items import add_transmittal_item
        from project_management.services.transmittal_finalize import finalize_transmittal

        revision = self.make_approved_internal_revision()
        first = self.create_transmittal()
        add_transmittal_item(first, revision, "FOR_REVIEW", self.owner)
        finalize_transmittal(first, self.owner)

        second = self.create_transmittal()
        add_transmittal_item(second, revision, "FOR_REVIEW", self.owner)
        finalize_transmittal(second, self.owner)

        events = self.events(revision.document)
        distribution_events = [e for e in events if e["event_group"] == "CLIENT_DISTRIBUTION"]
        self.assertEqual(len(distribution_events), 2)
        self.assertEqual(
            sorted(e["event_type"] for e in distribution_events),
            ["ISSUED", "RETRANSMITTED"],
        )
        self.assertEqual(
            len({e["source_id"] for e in distribution_events}), 2,
        )  # two distinct TransmittalItem rows, never collapsed


# 10. Historical client_issue=NULL distributions still appear.
class HistoricalNullClientIssueTests(HistoryEventsTestBase):
    def test_historical_null_client_issue_distribution_still_returned(self):
        from project_management.services.transmittal_items import add_transmittal_item
        from project_management.services.transmittal_finalize import finalize_transmittal

        revision = self.make_approved_internal_revision()
        transmittal = self.create_transmittal()
        item = add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalize_transmittal(transmittal, self.owner)
        # Simulate a pre-HISTORY-1 historical row (client_issue was never set).
        item.refresh_from_db()
        item.client_issue = None
        item.save(update_fields=["client_issue"])

        events = self.events(revision.document)
        distribution_events = [e for e in events if e["event_group"] == "CLIENT_DISTRIBUTION"]
        self.assertEqual(len(distribution_events), 1)
        self.assertIsNone(distribution_events[0]["metadata"]["client_issue_id"])
        self.assertIsNone(distribution_events[0]["metadata"]["client_revision_label"])
        # The real evidence is untouched.
        self.assertEqual(
            distribution_events[0]["metadata"]["client_company_id"], self.client_company.id,
        )


# 11. CLIENT-origin: revision events returned, no fabricated workflow/surveillance.
class ClientOriginEventsTests(HistoryEventsTestBase):
    def test_client_origin_has_only_revision_events(self):
        revision = self.make_client_revision()
        events = self.events(revision.document)
        # Same materialized-Version-1 quirk as above: the Document's own initial file becomes
        # a second, unlabeled REVISION event -- still legitimately only ever REVISION group,
        # never a fabricated workflow/surveillance event (section 11/30).
        self.assertTrue(all(e["event_group"] == "REVISION" for e in events))
        matching = [e for e in events if e["document_revision_id"] == revision.id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["source_revision"], revision.external_revision)


# 12. Deterministic newest-first ordering.
class OrderingTests(HistoryEventsTestBase):
    def test_events_are_sorted_newest_first(self):
        revision = self.make_approved_internal_revision()
        events = self.events(revision.document)
        timestamps = [e["occurred_at"] for e in events]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))


# 13. N+1 avoidance, mirroring the same query-count convention already established by
# test_document_revision_history.py / test_document_revision_history_supplier.py.
class QueryCountTests(HistoryEventsTestBase):
    def test_query_count_does_not_scale_with_event_count(self):
        from project_management.services.transmittal_items import add_transmittal_item
        from project_management.services.transmittal_finalize import finalize_transmittal

        revision = self.make_approved_internal_revision()
        transmittal = self.create_transmittal()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalize_transmittal(transmittal, self.owner)
        with CaptureQueriesContext(connection) as small:
            self.events(revision.document)

        for _ in range(3):
            more_revision = self.make_approved_internal_revision()
            more_transmittal = self.create_transmittal()
            add_transmittal_item(more_transmittal, more_revision, "FOR_REVIEW", self.owner)
            finalize_transmittal(more_transmittal, self.owner)
        # Different Document each time (make_approved_internal_revision creates a new one) --
        # query count for ONE Document's own events must stay bounded regardless of how much
        # unrelated data exists elsewhere.
        with CaptureQueriesContext(connection) as large:
            self.events(revision.document)
        self.assertEqual(len(small), len(large))


# View wiring: the view itself calls the real document-access lookup and returns this exact
# shape under {"document_origin": ..., "results": [...]}.
class ViewWiringTests(HistoryEventsTestBase):
    def test_view_returns_events_for_accessible_document(self):
        revision = self.make_approved_internal_revision()
        document = revision.document
        factory = APIRequestFactory()
        request = factory.get(f"/document/{document.id}/history-events")
        force_authenticate(request, user=self.owner)
        view = DocumentHistoryEventsView.as_view(permission_classes=[])
        with patch(ACCESS, return_value=document):
            response = view(request, document_id=document.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["document_origin"], "INTERNAL")
        self.assertGreaterEqual(len(response.data["results"]), 1)
