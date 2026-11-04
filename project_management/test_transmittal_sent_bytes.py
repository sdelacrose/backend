import hashlib
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import override_settings
from project_management.test_transmittal import TransmittalTestBase
from project_management.services.transmittal_items import add_transmittal_item
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_send import send_transmittal

@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',DEFAULT_FROM_EMAIL='links@test.invalid')
class TransmittalSentBytesTests(TransmittalTestBase):
    def setUp(self):
        super().setUp()
        self.revision=self.make_approved_internal_revision()
        self.transmittal=self.create_transmittal(subject='Dedicated distribution integrity test')
        self.transmittal.external_contacts.add(self.client_contact)
        add_transmittal_item(self.transmittal,self.revision,'FOR_INFORMATION',self.owner)
        self.transmittal=finalize_transmittal(self.transmittal,self.owner)

    def test_transport_receives_verified_bytes_even_if_disk_changes_after_validation(self):
        with self.revision.file.open('rb') as stream: expected=stream.read()
        received=[]
        def transport(**payload):
            with open(self.revision.file.path,'wb') as stream: stream.write(b'Changed on disk after verification')
            with payload['attachments'][0].file.open('rb') as stream: received.append(stream.read())
            return 'test-message-id'
        with patch('project_management.services.transmittal_send._deliver_via_provider',side_effect=transport):
            event=send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.client_contact.pk)
        self.assertEqual(event.event_type,'SEND_SUCCEEDED')
        self.assertEqual(received,[expected])
        evidence=self.transmittal.events.get(event_type='SEND_STARTED').metadata['attachments'][0]
        self.assertEqual(evidence['sha256'],hashlib.sha256(received[0]).hexdigest())

    def test_unreadable_file_is_a_validation_refusal_without_an_attempt(self):
        with patch('django.core.files.storage.filesystem.FileSystemStorage._open',side_effect=FileNotFoundError('not found')):
            with self.assertRaises(ValidationError):
                send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.client_contact.pk)
        self.assertFalse(self.transmittal.events.filter(event_type='SEND_STARTED').exists())

    def test_corrupt_file_is_refused_before_transport(self):
        with open(self.revision.file.path,'wb') as stream: stream.write(b'Corrupt stored file')
        with patch('project_management.services.transmittal_send._deliver_via_provider') as transport:
            with self.assertRaises(ValidationError): send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.client_contact.pk)
        transport.assert_not_called()
        self.assertFalse(self.transmittal.events.filter(event_type='SEND_STARTED').exists())

    def test_smtp_backend_that_accepts_zero_messages_cannot_mark_transmittal_sent(self):
        with patch('project_management.services.correspondence_send.EmailMultiAlternatives.send',return_value=0):
            event=send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.client_contact.pk)
        self.assertEqual(event.event_type,'SEND_FAILED')
        self.assertTrue(event.metadata['uncertain'])

    def test_frozen_snapshot_hash_is_checked_before_any_send_attempt(self):
        import json
        from django.db import connection
        item = self.transmittal.items.first()
        snapshot = item.evidence_snapshot
        snapshot['revision']['file_sha256'] = '0' * 64
        # Simulate inconsistent stored evidence; normal issued items are immutable.
        with connection.cursor() as cursor:
            cursor.execute('UPDATE project_management_transmittalitem SET evidence_snapshot = %s WHERE id = %s', [json.dumps(snapshot), item.pk])
        with patch('project_management.services.transmittal_send._deliver_via_provider') as transport:
            with self.assertRaises(ValidationError): send_transmittal(self.transmittal,self.owner)
        transport.assert_not_called()
        self.assertFalse(self.transmittal.events.filter(event_type='SEND_STARTED').exists())
