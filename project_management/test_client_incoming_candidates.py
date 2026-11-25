from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase
from project_management.services.client_surveillance_return import (
    incoming_transmittal_candidate, incoming_transmittal_candidates, eligible_in_transmittals,
)

class ClientIncomingCandidateTests(SimpleTestCase):
    def row(self, **overrides):
        values = dict(pk=1, transmittal_number='IN-1', subject='Review comments',
                      external_company_id=29, external_company=SimpleNamespace(company_name='Client A'),
                      status='ISSUED', received_at=datetime(2026, 11, 14, tzinfo=timezone.utc))
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_confirmed_same_client_can_be_associated(self):
        value = incoming_transmittal_candidate(self.row(), 29)
        self.assertTrue(value['eligible'])
        self.assertIsNone(value['blocked_reason'])
        self.assertEqual(value['subject'], 'Review comments')

    def test_same_client_draft_remains_visible_but_requires_receipt(self):
        value = incoming_transmittal_candidate(self.row(status='DRAFT', received_at=None), 29)
        self.assertTrue(value['same_client'])
        self.assertFalse(value['eligible'])
        self.assertEqual(value['blocked_reason'], 'RECEIPT_REQUIRED')

    def test_other_client_never_becomes_associable_even_after_confirmation(self):
        value = incoming_transmittal_candidate(self.row(), 31)
        self.assertFalse(value['same_client'])
        self.assertFalse(value['eligible'])
        self.assertEqual(value['blocked_reason'], 'WRONG_CLIENT')

    def test_no_received_date_cannot_be_associated(self):
        value = incoming_transmittal_candidate(self.row(received_at=None), 29)
        self.assertFalse(value['eligible'])

    @patch('project_management.services.client_surveillance_return.Transmittal.objects')
    def test_discovery_is_project_scoped_and_association_keeps_stricter_filters(self, objects):
        incoming_transmittal_candidates(19)
        objects.filter.assert_called_with(project_id=19, direction='IN')
        eligible_in_transmittals(SimpleNamespace(client_company_id=29), 19)
        objects.filter.assert_called_with(project_id=19, direction='IN', status='ISSUED',
                                         external_company_id=29, received_at__isnull=False)
