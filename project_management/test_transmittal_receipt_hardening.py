"""GED-WF.3.1: an incoming transmittal can only be received through the receipt confirmation.

No other route may flip a draft to ISSUED (the `issue` action, a direct status update), whatever the
rights of the caller, and the confirmation itself stays atomic: a failure leaves nothing behind.
"""

from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import Document, DocumentRevision, Transmittal, TransmittalEvent
from project_management.services import transmittal_receipt
from project_management.test_transmittal_receipt import ReceiptBase
from project_management.views.transmittal import (
    TransmittalConfirmReceiptView, TransmittalIssueView, TransmittalViewSet,
)


class IncomingReceiptOnlyThroughConfirmation(ReceiptBase):
    def patch_status(self, transmittal, user, status):
        request = APIRequestFactory().patch("/x/", {"status": status}, format="json")
        force_authenticate(request, user=user)
        return TransmittalViewSet.as_view({"patch": "partial_update"}, permission_classes=[])(request, pk=transmittal.pk)

    # issue, whatever the rights: 400 and nothing written
    def test_issue_is_refused_for_an_incoming_transmittal_whatever_the_rights(self):
        transmittal = self.in_draft("H-1", self.supplier_doc("HRD-001")[1])
        for email, actions in (
            ("h.issue@links-local.invalid", ("issue",)),
            ("h.issue.update@links-local.invalid", ("issue", "update")),
            ("h.full@links-local.invalid", ("full_access",)),
        ):
            user = self.user_with(email, self.project, actions)
            response = self.post(TransmittalIssueView, transmittal.pk, user)
            self.assertEqual(response.status_code, 400, (email, getattr(response, "data", None)))
        stored = Transmittal.objects.get(pk=transmittal.pk)
        self.assertEqual(stored.status, "DRAFT")
        self.assertEqual(self.received_events(transmittal), 0)

    # a direct status update must not be a back door to "received" (nor to "issued")
    def test_a_direct_status_update_cannot_receive_or_issue(self):
        user = self.user_with("h.updater@links-local.invalid", self.project, ("update", "issue"))
        incoming = self.in_draft("H-2", self.supplier_doc("HRD-002")[1])
        response = self.patch_status(incoming, user, "ISSUED")
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))
        self.assertEqual(Transmittal.objects.get(pk=incoming.pk).status, "DRAFT")
        self.assertEqual(self.received_events(incoming), 0)
        outgoing = self.create_out_transmittal()
        response = self.patch_status(outgoing, user, "ISSUED")
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))
        self.assertEqual(Transmittal.objects.get(pk=outgoing.pk).status, "DRAFT")

    # the header can still be edited without sending a status (unchanged behaviour)
    def test_header_update_without_status_still_works(self):
        user = self.user_with("h.header@links-local.invalid", self.project, ("update",))
        transmittal = self.in_draft("H-3", self.supplier_doc("HRD-003")[1])
        request = APIRequestFactory().patch("/x/", {"subject": "Updated subject"}, format="json")
        force_authenticate(request, user=user)
        response = TransmittalViewSet.as_view({"patch": "partial_update"}, permission_classes=[])(request, pk=transmittal.pk)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(Transmittal.objects.get(pk=transmittal.pk).subject, "Updated subject")

    # the finalization service is reached only through the receipt confirmation for an incoming one
    def test_finalize_is_only_called_for_an_incoming_transmittal_by_the_confirmation(self):
        user = self.user_with("h.confirm@links-local.invalid", self.project, ("update",))
        transmittal = self.in_draft("H-4", self.supplier_doc("HRD-004")[1])
        with mock.patch.object(transmittal_receipt, "finalize_transmittal", wraps=transmittal_receipt.finalize_transmittal) as spy:
            self.assertEqual(self.post(TransmittalConfirmReceiptView, transmittal.pk, user).status_code, 200)
        self.assertEqual(spy.call_count, 1)

    # atomic: a failure inside the finalization leaves the draft untouched, no event, no evidence
    def test_a_failure_during_the_confirmation_rolls_everything_back(self):
        user = self.user_with("h.rollback@links-local.invalid", self.project, ("update",))
        transmittal = self.in_draft("H-5", self.supplier_doc("HRD-005")[1])

        def boom(locked, actor):
            # write something first, as the real finalization does, then fail
            TransmittalEvent.objects.create(transmittal=locked, event_type="RECEIVED", actor=actor)
            raise RuntimeError("simulated failure")

        with mock.patch.object(transmittal_receipt, "finalize_transmittal", side_effect=boom):
            with self.assertRaises(RuntimeError):
                transmittal_receipt.confirm_incoming_receipt(transmittal, user)
        stored = Transmittal.objects.get(pk=transmittal.pk)
        self.assertEqual(stored.status, "DRAFT")
        self.assertIsNone(stored.received_at)
        self.assertEqual(self.received_events(transmittal), 0)
        self.assertFalse(stored.items.filter(evidence_snapshot__isnull=False).exists())
        # and the very same draft can then be confirmed normally
        self.assertEqual(self.post(TransmittalConfirmReceiptView, transmittal.pk, user).status_code, 200)
