# CLIENT-DISTRIBUTION-DUE-DATE-FIX: backend rule behind the form. Every distribution mode except "Not sent" needs a due date
# (project deliverables of internal or supplier origin); "Not sent" never keeps one; a request that does not touch the mode or the
# date never fails because of it, and a historical date is never rewritten. No migration, no new field, no mail.
import datetime

from project_management.models import Document
from project_management.test_document_origin import OriginBase

DATE = "2026-12-24"
MODES = ["Available", "For review", "For information", "FOR_REVIEW", "For construction"]


class ClientDueDateTests(OriginBase):
    def test_every_mode_but_not_sent_requires_a_date_on_creation(self):
        for mode in MODES:
            errors = self.errors_of(self.payload(transmittal_category=mode))
            self.assertIn("due_date", errors, mode)
            document = self.create(transmittal_category=mode, due_date=DATE)
            self.assertEqual((document.transmittal_category, document.due_date), (mode, datetime.date(2026, 12, 24)))

    def test_not_sent_is_saved_without_a_date_and_never_keeps_one(self):
        for mode in ("Not sent", "NOT_SENT"):
            document = self.create(transmittal_category=mode)
            self.assertIsNone(document.due_date)
            residual = self.create(transmittal_category=mode, due_date=DATE)
            self.assertIsNone(residual.due_date)                       # a residual date sent with it is dropped

    def test_no_mode_needs_no_date_and_keeps_the_historical_behaviour(self):
        for data in ({}, {"transmittal_category": ""}):
            self.assertIsNone(self.create(**data).due_date)

    def test_an_empty_or_missing_date_is_refused_in_both_forms(self):
        for value in ("", None):
            self.assertIn("due_date", self.errors_of(self.payload(transmittal_category="For review", due_date=value)))

    def test_switching_the_mode_in_both_directions(self):
        document = self.create(transmittal_category="For review", due_date=DATE)
        # to Not sent: allowed without a date, the stale one is cleared when the form sends it empty
        self.assertEqual(self.update(document, transmittal_category="Not sent", due_date=None).due_date, None)
        document.refresh_from_db()
        self.assertIsNone(document.due_date)
        # back to a mode that needs a date: refused until one is given
        self.assertIn("due_date", self.errors_of({"transmittal_category": "Available"}, document, partial=True))
        self.assertEqual(self.update(document, transmittal_category="Available", due_date="2027-01-15").due_date, datetime.date(2027, 1, 15))

    def test_a_date_cannot_be_removed_from_a_document_that_needs_one(self):
        document = self.create(transmittal_category="For information", due_date=DATE)
        self.assertIn("due_date", self.errors_of({"due_date": None}, document, partial=True))

    def test_historical_documents_are_never_refused_for_an_unrelated_edit(self):
        # created before the rule: a mode and no date, written without any validation
        document = self.create(transmittal_category="Available", due_date=DATE)
        Document.objects.filter(pk=document.pk).update(due_date=None)
        document.refresh_from_db()
        self.assertEqual(self.update(document, description="unrelated edit").description, "unrelated edit")
        document.refresh_from_db()
        self.assertEqual((document.transmittal_category, document.due_date), ("Available", None))
        # the same rule applies as soon as the mode itself is part of the request
        self.assertIn("due_date", self.errors_of({"transmittal_category": "Available"}, document, partial=True))

    def test_a_historical_date_is_preserved(self):
        document = self.create(transmittal_category="Not sent")
        Document.objects.filter(pk=document.pk).update(due_date=datetime.date(2026, 1, 5))
        document.refresh_from_db()
        self.update(document, description="edit")
        document.refresh_from_db()
        self.assertEqual(document.due_date, datetime.date(2026, 1, 5))

    def test_client_origin_documents_are_out_of_scope(self):
        issuer = self.client_company.id
        document = self.create(document_origin="CLIENT", external_issuer_company=issuer, transmittal_category="For review")
        self.assertIsNone(document.due_date)

    def test_supplier_origin_follows_the_same_rule(self):
        issuer = self.supplier_company.id
        self.assertIn("due_date", self.errors_of(self.payload(document_origin="SUPPLIER", external_issuer_company=issuer, transmittal_category="Available")))
        document = self.create(document_origin="SUPPLIER", external_issuer_company=issuer, transmittal_category="Available", due_date=DATE)
        self.assertEqual(document.due_date, datetime.date(2026, 12, 24))

    def test_the_date_does_not_start_any_client_surveillance(self):
        from project_management.models import ClientSurveillance, ClientSurveillanceFollowUp

        before = (ClientSurveillance.objects.count(), ClientSurveillanceFollowUp.objects.count())
        self.create(transmittal_category="For review", due_date=DATE)
        self.assertEqual((ClientSurveillance.objects.count(), ClientSurveillanceFollowUp.objects.count()), before)
