"""HISTORY-1: ClientIssue foundation -- targeted tests only. No History UI, no Register
change, no Client Review workflow, no comments, no automatic numbering (all future lots,
deliberately not covered here).
"""

from django.core.exceptions import ValidationError

from project_management.models import ClientIssue, ContactCompany
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_items import add_transmittal_item
from project_management.test_transmittal import TransmittalTestBase


# 1/3. First successful OUT issuance creates a ClientIssue; a failed finalization creates none.
class CreationOnFinalizationTests(TransmittalTestBase):
    def test_first_out_issuance_creates_client_issue_with_null_label(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        self.assertIsNotNone(item.client_issue_id)
        self.assertEqual(item.client_issue.document_revision_id, revision.id)
        self.assertEqual(item.client_issue.client_company_id, self.client_company.id)
        self.assertIsNone(item.client_issue.client_revision_label)
        self.assertEqual(ClientIssue.objects.count(), 1)

    def test_failed_finalization_creates_no_orphan_client_issue(self):
        transmittal = self.create_transmittal()
        # No items at all -> finalize_transmittal raises before any ClientIssue work.
        with self.assertRaises(Exception):
            finalize_transmittal(transmittal, self.owner)
        self.assertEqual(ClientIssue.objects.count(), 0)


# 2. A draft transmittal (items added, never finalized) creates no ClientIssue.
class DraftTransmittalTests(TransmittalTestBase):
    def test_draft_transmittal_creates_no_client_issue(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        self.assertEqual(ClientIssue.objects.count(), 0)


# 4/5/6. Identity rules: retransmission reuses, a new source revision or a different Client
# company each gets its own row.
class IdentityRulesTests(TransmittalTestBase):
    def test_retransmission_to_same_client_reuses_client_issue(self):
        revision = self.make_approved_internal_revision()
        first = self.create_transmittal()
        add_transmittal_item(first, revision, "FOR_REVIEW", self.owner)
        finalized_first = finalize_transmittal(first, self.owner)

        second = self.create_transmittal()
        add_transmittal_item(second, revision, "FOR_REVIEW", self.owner)
        finalized_second = finalize_transmittal(second, self.owner)

        item1 = finalized_first.items.get()
        item2 = finalized_second.items.get()
        self.assertEqual(item1.client_issue_id, item2.client_issue_id)
        self.assertEqual(ClientIssue.objects.count(), 1)

    def test_new_source_revision_gets_a_different_client_issue(self):
        revision_b = self.make_approved_internal_revision()
        transmittal_b = self.create_transmittal()
        add_transmittal_item(transmittal_b, revision_b, "FOR_REVIEW", self.owner)
        finalized_b = finalize_transmittal(transmittal_b, self.owner)

        revision_c = self.make_approved_internal_revision()
        transmittal_c = self.create_transmittal()
        add_transmittal_item(transmittal_c, revision_c, "FOR_REVIEW", self.owner)
        finalized_c = finalize_transmittal(transmittal_c, self.owner)

        item_b = finalized_b.items.get()
        item_c = finalized_c.items.get()
        self.assertNotEqual(item_b.client_issue_id, item_c.client_issue_id)
        self.assertEqual(ClientIssue.objects.count(), 2)

    def test_different_client_company_gets_a_different_client_issue(self):
        other_client = ContactCompany.objects.create(
            company_profile=self.company, company_name="Other Client", company_role="Client",
            contact_email="other.client@x.invalid",
        )
        revision = self.make_approved_internal_revision()

        to_acme = self.create_transmittal()
        add_transmittal_item(to_acme, revision, "FOR_REVIEW", self.owner)
        finalized_acme = finalize_transmittal(to_acme, self.owner)

        to_other = self.create_transmittal(external_company=other_client.id)
        add_transmittal_item(to_other, revision, "FOR_REVIEW", self.owner)
        finalized_other = finalize_transmittal(to_other, self.owner)

        item_acme = finalized_acme.items.get()
        item_other = finalized_other.items.get()
        self.assertNotEqual(item_acme.client_issue_id, item_other.client_issue_id)
        self.assertEqual(item_acme.client_issue.client_company_id, self.client_company.id)
        self.assertEqual(item_other.client_issue.client_company_id, other_client.id)
        self.assertEqual(ClientIssue.objects.count(), 2)


# 7/8. FOR_INFORMATION still creates/reuses a ClientIssue (no deadline); FOR_REVIEW keeps the
# existing L3H-3 deadline snapshot untouched, alongside the new ClientIssue link.
class DistributionPurposeTests(TransmittalTestBase):
    def test_for_information_creates_client_issue_with_no_review_deadline(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        self.assertIsNotNone(item.client_issue_id)
        self.assertIsNone(item.review_duration_days)
        self.assertIsNone(item.review_due_date)

    def test_for_review_creates_client_issue_and_keeps_review_deadline(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        self.assertIsNotNone(item.client_issue_id)
        self.assertIsNotNone(item.review_duration_days)
        self.assertIsNotNone(item.review_due_date)


# 9/10. Backward compatibility: a historical item with no ClientIssue stays valid; CLIENT-origin
# eligibility is unchanged (still rejected outright, well before any ClientIssue code runs).
class BackwardCompatibilityTests(TransmittalTestBase):
    def test_historical_item_with_null_client_issue_remains_valid(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        item = add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        # Never finalized: exactly the pre-HISTORY-1 shape every existing DRAFT item still has.
        item.refresh_from_db()
        self.assertIsNone(item.client_issue_id)

    def test_client_origin_document_still_ineligible_for_out(self):
        revision = self.make_client_revision()
        transmittal = self.create_transmittal()
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        self.assertEqual(ClientIssue.objects.count(), 0)
