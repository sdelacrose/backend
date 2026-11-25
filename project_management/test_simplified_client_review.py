from project_management.models import ClientSurveillance, ClientSurveillanceReturn
from project_management.test_client_surveillance_return import ReturnBase


class SimplifiedClientReviewTests(ReturnBase):
    def test_recording_a_return_advances_the_open_review_without_status_clicks(self):
        self.assertEqual(self.surveillance.status, 'UNDER_REVIEW')
        record = self.record(category='ACCEPTED')
        self.surveillance.refresh_from_db()
        self.assertEqual(self.surveillance.status, 'READY_FOR_CLOSURE')
        event = self.surveillance.events.get(event_type='RETURN_RECORDED')
        self.assertTrue(event.data['automatic'])
        self.assertEqual(event.data['previous_status'], 'UNDER_REVIEW')
        self.assertEqual(event.data['return'], record.pk)

    def test_legacy_review_can_close_with_result_without_intermediate_transitions(self):
        # An existing historical return, recorded before automatic follow-ups.
        record = ClientSurveillanceReturn.objects.create(client_issue=self.client_issue, category='ACCEPTED',
            received_at=self.now(), source_channel='EMAIL', recorded_by=self.manager)
        response = self.patch(self.manager, {'status': 'CLOSED', 'retained_return': record.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.surveillance.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.result), ('CLOSED', 'ACCEPTED'))

    def test_missing_surveillance_is_created_by_the_return_without_a_creation_click(self):
        revision = self.review()
        transmittal = self.issue([(revision, 'FOR_REVIEW')])  # historical issued distribution without a tracked send
        issue = transmittal.items.get().client_issue
        self.assertFalse(ClientSurveillance.objects.filter(client_issue=issue).exists())
        self.record(issue=issue, category='ACCEPTED')
        surveillance = ClientSurveillance.objects.get(client_issue=issue)
        self.assertEqual(surveillance.status, 'READY_FOR_CLOSURE')
        self.assertIsNone(surveillance.opened_at)
        self.assertIsNone(surveillance.due_at)
        self.assertEqual(surveillance.events.filter(event_type='OPENED').count(), 1)

    def test_recording_a_late_return_keeps_a_closed_review_closed(self):
        ClientSurveillance.objects.filter(pk=self.surveillance.pk).update(status='CLOSED')
        self.record(category='ACCEPTED')
        self.surveillance.refresh_from_db()
        self.assertEqual(self.surveillance.status, 'CLOSED')
        self.assertEqual(self.surveillance.events.filter(event_type='RETURN_LATE').count(), 1)
