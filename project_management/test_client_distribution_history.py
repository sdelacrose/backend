"""HISTORY-2: Client Distribution History read path -- targeted tests only. No History UI
business logic lives here (the frontend only renders what this service returns); no Client
Review/comments/numbering (future lots, deliberately not covered).
"""

from datetime import timedelta
from unittest import mock

from django.http import Http404
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.services.client_distribution_history import (
    is_actual_client_recipient,
    list_client_distribution_history,
)
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_items import add_transmittal_item
from project_management.test_transmittal import TransmittalTestBase
from project_management.views.client_distribution_history import (
    ClientDistributionHistoryView,
)


class ClientDistributionHistoryTestBase(TransmittalTestBase):
    def issue(self, revision, purpose="FOR_REVIEW"):
        transmittal = self.create_transmittal()
        add_transmittal_item(transmittal, revision, purpose, self.owner)
        return finalize_transmittal(transmittal, self.owner)


# 1/2. Only an actually ISSUED OUT item appears; a DRAFT item never does.
class IssuedVsDraftTests(ClientDistributionHistoryTestBase):
    def test_issued_out_item_appears(self):
        revision = self.make_approved_internal_revision()
        self.issue(revision)
        results = list_client_distribution_history(revision.document)
        self.assertEqual(len(results), 1)

    def test_draft_out_item_does_not_appear(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        results = list_client_distribution_history(revision.document)
        self.assertEqual(results, [])


# 3/4. Source revision is origin-aware; Client company is correctly reported.
class SourceRevisionAndClientCompanyTests(ClientDistributionHistoryTestBase):
    def test_internal_source_revision_is_revision_label(self):
        revision = self.make_approved_internal_revision()
        self.issue(revision)
        result = list_client_distribution_history(revision.document)[0]
        self.assertEqual(result["source_revision"], revision.revision_label)
        self.assertIsNotNone(result["source_revision"])

    def test_supplier_source_revision_is_external_revision(self):
        revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        self.make_surveillance(revision, "CLOSED", result="REVIEWED")
        self.issue(revision)
        result = list_client_distribution_history(revision.document)[0]
        self.assertEqual(result["source_revision"], revision.external_revision)

    def test_client_company_reported(self):
        revision = self.make_approved_internal_revision()
        self.issue(revision)
        result = list_client_distribution_history(revision.document)[0]
        self.assertEqual(result["client_company_id"], self.client_company.id)
        self.assertEqual(result["client_company_name"], self.client_company.company_name)


# 5/6. ClientIssue label surfaces when present; a NULL client_issue (pre-HISTORY-1 historical
# item) still appears, with a NULL Client Revision label -- never fabricated.
class ClientIssueLabelTests(ClientDistributionHistoryTestBase):
    def test_client_revision_label_returned_when_present(self):
        revision = self.make_approved_internal_revision()
        finalized = self.issue(revision)
        item = finalized.items.get()
        item.client_issue.client_revision_label = "A"
        item.client_issue.save(update_fields=["client_revision_label"])
        result = list_client_distribution_history(revision.document)[0]
        self.assertEqual(result["client_revision_label"], "A")

    def test_historical_null_client_issue_item_still_appears_with_null_label(self):
        revision = self.make_approved_internal_revision()
        finalized = self.issue(revision)
        item = finalized.items.get()
        # Simulate a pre-HISTORY-1 historical row (section 11): client_issue was never set.
        item.client_issue = None
        item.save(update_fields=["client_issue"])
        result = list_client_distribution_history(revision.document)[0]
        self.assertIsNone(result["client_issue_id"])
        self.assertIsNone(result["client_revision_label"])
        # The real evidence is untouched: company/reference/dates still come from the
        # Transmittal/DocumentRevision themselves.
        self.assertEqual(result["client_company_id"], self.client_company.id)
        self.assertEqual(result["source_revision"], revision.revision_label)


# 7/8. FOR_REVIEW keeps its deadline/overdue condition; FOR_INFORMATION has none.
class DeadlineTests(ClientDistributionHistoryTestBase):
    def test_for_review_deadline_and_overdue_reported(self):
        revision = self.make_approved_internal_revision()
        finalized = self.issue(revision, purpose="FOR_REVIEW")
        item = finalized.items.get()
        item.review_due_date = item.review_due_date - timedelta(days=100)
        item.save(update_fields=["review_due_date"])
        result = list_client_distribution_history(revision.document)[0]
        self.assertIsNotNone(result["review_due_date"])
        self.assertTrue(result["is_overdue"])

    def test_for_information_has_no_review_deadline(self):
        revision = self.make_approved_internal_revision()
        self.issue(revision, purpose="FOR_INFORMATION")
        result = list_client_distribution_history(revision.document)[0]
        self.assertIsNone(result["review_due_date"])
        self.assertFalse(result["is_overdue"])


# 9. Both retransmission occurrences appear, referencing the same ClientIssue.
class RetransmissionTests(ClientDistributionHistoryTestBase):
    def test_both_retransmission_occurrences_appear_with_same_client_issue(self):
        revision = self.make_approved_internal_revision()
        self.issue(revision)
        self.issue(revision)
        results = list_client_distribution_history(revision.document)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["client_issue_id"], results[1]["client_issue_id"])
        self.assertNotEqual(results[0]["transmittal_id"], results[1]["transmittal_id"])


# 10. Another Document's distributions never leak into this Document's result.
class IsolationTests(ClientDistributionHistoryTestBase):
    def test_other_document_distributions_do_not_leak(self):
        revision = self.make_approved_internal_revision()
        self.issue(revision)
        other_revision = self.make_approved_internal_revision()
        self.issue(other_revision)
        results = list_client_distribution_history(revision.document)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["document_revision_id"], revision.id)


# Read-path wiring: the view itself calls the real document-access lookup (the exact same
# helper DocumentRevisionViewSet already uses -- its own scoping/RBAC is that module's concern,
# already covered by its own tests, and deliberately not re-fixtured here) and then the real
# service, returning its exact shape under {"results": [...]}.
class ViewWiringTests(ClientDistributionHistoryTestBase):
    def test_view_returns_distribution_history_for_accessible_document(self):
        revision = self.make_approved_internal_revision()
        self.issue(revision)
        factory = APIRequestFactory()
        request = factory.get(f"/document/{revision.document_id}/client-distribution-history")
        force_authenticate(request, user=self.owner)
        # Same test-only bypass of the global LicensePermission default already established by
        # test_document_type_review_duration.py (the test DB has no package_license fixture) --
        # never a production behavior change.
        view = ClientDistributionHistoryView.as_view(permission_classes=[])
        document = revision.document
        with mock.patch(
            "project_management.views.client_distribution_history.get_register_document_or_404",
            return_value=document,
        ) as mocked_lookup:
            response = view(request, document_id=document.id)
        mocked_lookup.assert_called_once_with(mock.ANY, document_id=document.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)

    def test_view_404s_when_the_document_lookup_rejects_access(self):
        revision = self.make_approved_internal_revision()
        self.issue(revision)
        factory = APIRequestFactory()
        request = factory.get(f"/document/{revision.document_id}/client-distribution-history")
        force_authenticate(request, user=self.owner)
        view = ClientDistributionHistoryView.as_view(permission_classes=[])
        with mock.patch(
            "project_management.views.client_distribution_history.get_register_document_or_404",
            side_effect=Http404,
        ):
            response = view(request, document_id=revision.document_id)
        self.assertEqual(response.status_code, 404)


# HISTORY-4.1: an OUT+ISSUED recipient is not necessarily the Client -- a document sent back
# to the exact Supplier company that issued it to us (e.g. a future comment-return flow) must
# never be presented as a Client distribution. The shared rule lives in
# is_actual_client_recipient and is used by both this service and services.
# document_history_events -- never two divergent definitions (section 6).
class ActualClientRecipientTests(ClientDistributionHistoryTestBase):
    def make_supplier_document_with_issuer(self):
        document = self.make_document(origin="SUPPLIER", name="s.txt")
        document.external_issuer_company = self.supplier_company
        document.supplier_transmittal_category = "FOR_INFORMATION"
        document.transmittal_category = "For information"
        document.save(
            update_fields=[
                "external_issuer_company", "supplier_transmittal_category", "transmittal_category",
            ]
        )
        return self.add_revision(document, content=b"supplier-v1")

    # 1/2. A real Client recipient keeps appearing in both read paths (already covered
    # elsewhere by the main suite; re-asserted here directly against the shared predicate).
    def test_actual_client_company_is_accepted(self):
        revision = self.make_supplier_document_with_issuer()
        self.assertTrue(
            is_actual_client_recipient(revision.document, self.client_company.id)
        )

    # 3/4. An OUT addressed back to the exact Supplier that issued this Document is excluded
    # from Client Distribution History.
    def test_out_to_the_documents_own_supplier_is_excluded_from_distribution_history(self):
        revision = self.make_supplier_document_with_issuer()
        self.issue(revision, purpose="FOR_INFORMATION")  # to self.client_company: a real one
        back_to_supplier = self.create_transmittal(external_company=self.supplier_company.id)
        add_transmittal_item(back_to_supplier, revision, "FOR_INFORMATION", self.owner)
        finalize_transmittal(back_to_supplier, self.owner)

        results = list_client_distribution_history(revision.document)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["client_company_id"], self.client_company.id)

    def test_out_to_the_documents_own_supplier_is_excluded_from_all_events(self):
        from project_management.services.document_history_events import (
            list_document_history_events,
        )

        revision = self.make_supplier_document_with_issuer()
        self.issue(revision, purpose="FOR_INFORMATION")
        back_to_supplier = self.create_transmittal(external_company=self.supplier_company.id)
        add_transmittal_item(back_to_supplier, revision, "FOR_INFORMATION", self.owner)
        finalize_transmittal(back_to_supplier, self.owner)

        events = list_document_history_events(revision.document)
        distribution_events = [e for e in events if e["event_group"] == "CLIENT_DISTRIBUTION"]
        self.assertEqual(len(distribution_events), 1)
        self.assertEqual(
            distribution_events[0]["metadata"]["client_company_id"], self.client_company.id,
        )

    # 5. A historical client_issue=NULL Client distribution is unaffected by this rule.
    def test_historical_null_client_issue_distribution_still_included(self):
        revision = self.make_approved_internal_revision()
        finalized = self.issue(revision)
        item = finalized.items.get()
        item.client_issue = None
        item.save(update_fields=["client_issue"])

        results = list_client_distribution_history(revision.document)
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["client_issue_id"])

    # 6. Without a recorded issuer at all (e.g. an INTERNAL document, or a SUPPLIER document
    # whose issuer was never set), every OUT recipient is accepted -- never excluded by
    # default.
    def test_no_recorded_issuer_never_excludes_a_recipient(self):
        revision = self.make_approved_internal_revision()
        self.assertTrue(
            is_actual_client_recipient(revision.document, self.supplier_company.id)
        )

    # 6. A company that is this Document's own Supplier issuer can still be a genuine Client
    # recipient for a DIFFERENT, unrelated Document -- the rule is per-Document, never a
    # global role tag on the company itself.
    def test_same_company_can_be_client_for_a_different_document(self):
        issuer_document_revision = self.make_supplier_document_with_issuer()
        other_revision = self.make_approved_internal_revision()
        self.assertFalse(
            is_actual_client_recipient(
                issuer_document_revision.document, self.supplier_company.id,
            )
        )
        self.assertTrue(
            is_actual_client_recipient(other_revision.document, self.supplier_company.id)
        )

    # 7. Retransmission behavior for a REAL Client distribution is unaffected by this rule.
    def test_retransmission_of_a_real_client_distribution_still_grouped_together(self):
        revision = self.make_approved_internal_revision()
        self.issue(revision)
        self.issue(revision)
        results = list_client_distribution_history(revision.document)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["client_company_id"], results[1]["client_company_id"])
