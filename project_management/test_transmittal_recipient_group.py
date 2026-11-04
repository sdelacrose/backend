from unittest.mock import patch
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import override_settings
from project_management.models import ContactUser
from project_management.test_transmittal import TransmittalTestBase
from project_management.services.transmittal_items import add_transmittal_item
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_send import send_transmittal

@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',DEFAULT_FROM_EMAIL='links@test.invalid')
class TransmittalRecipientGroupTests(TransmittalTestBase):
    def setUp(self):
        super().setUp()
        self.second=ContactUser.objects.create(company_profile=self.company,contact_company=self.client_company,name='Second',surname='Recipient',email='second@client.invalid')
        self.transmittal=self.create_transmittal(subject='Dedicated group distribution')
        self.transmittal.external_contacts.add(self.client_contact,self.second)
        add_transmittal_item(self.transmittal,self.make_approved_internal_revision(),'FOR_INFORMATION',self.owner)
        self.transmittal=finalize_transmittal(self.transmittal,self.owner)

    def test_one_message_reaches_every_selected_contact_and_records_recipients(self):
        event=send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.client_contact.pk)
        self.assertEqual(event.event_type,'SEND_SUCCEEDED')
        self.assertEqual(len(mail.outbox),1)
        self.assertEqual(mail.outbox[0].to,[self.client_contact.email])
        self.assertEqual(mail.outbox[0].cc,[self.second.email])
        evidence=self.transmittal.events.get(event_type='SEND_STARTED').metadata
        self.assertEqual(evidence['recipient_contacts'],[self.client_contact.pk,self.second.pk])
        self.assertEqual(evidence['cc'],[self.second.email])

    def test_primary_recipient_may_be_omitted(self):
        event=send_transmittal(self.transmittal,self.owner)
        self.assertEqual(event.event_type,'SEND_SUCCEEDED')
        self.assertEqual(set(mail.outbox[0].to+mail.outbox[0].cc),{self.client_contact.email,self.second.email})

    def test_a_selected_primary_does_not_drop_other_contacts(self):
        send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.second.pk)
        self.assertEqual(mail.outbox[0].to,[self.second.email])
        self.assertEqual(mail.outbox[0].cc,[self.client_contact.email])

    def test_missing_secondary_email_blocks_entire_send_before_attempt(self):
        self.second.email='';self.second.save(update_fields=['email'])
        with self.assertRaises(ValidationError):send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.client_contact.pk)
        self.assertFalse(self.transmittal.events.filter(event_type='SEND_STARTED').exists())
        self.assertEqual(mail.outbox,[])

    def test_contact_moved_to_other_company_blocks_entire_send(self):
        self.second.contact_company=self.supplier_company;self.second.save(update_fields=['contact_company'])
        with self.assertRaises(ValidationError):send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.client_contact.pk)
        self.assertFalse(self.transmittal.events.filter(event_type='SEND_STARTED').exists())

    def test_duplicate_email_addresses_are_sent_only_once(self):
        self.second.email=self.client_contact.email.upper();self.second.save(update_fields=['email'])
        send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.client_contact.pk)
        self.assertEqual(mail.outbox[0].to,[self.client_contact.email])
        self.assertEqual(mail.outbox[0].cc,[])

    def test_contact_outside_frozen_selection_cannot_be_added_by_send_payload(self):
        with self.assertRaises(ValidationError):send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.orphan_contact.pk)
        self.assertFalse(self.transmittal.events.filter(event_type='SEND_STARTED').exists())

    def test_graph_transport_receives_same_complete_group(self):
        from types import SimpleNamespace
        mailbox=SimpleNamespace(email_address='sender@links.invalid')
        with patch('project_management.services.transmittal_send.MailboxConnection.objects.filter') as query,patch('project_management.services.transmittal_send.validate_mailbox_for_send'),patch('project_management.services.transmittal_send._resolve_mail_sender_user',return_value=self.owner),patch('project_management.services.transmittal_send._deliver_via_graph',return_value='graph-test') as transport:
            query.return_value.first.return_value=mailbox
            event=send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.client_contact.pk,mailbox_id=999)
        self.assertEqual(event.event_type,'SEND_SUCCEEDED')
        self.assertEqual(transport.call_args.kwargs['to'],self.client_contact.email)
        self.assertEqual(transport.call_args.kwargs['cc'],[self.second.email])

    def test_real_graph_adapter_serializes_transmittal_attachment_before_http(self):
        import base64,hashlib
        from types import SimpleNamespace
        from userapis.models import MailboxConnection
        mailbox=SimpleNamespace(email_address='sender@links.invalid',connection_type=MailboxConnection.ConnectionType.SHARED)
        with patch('project_management.services.transmittal_send.MailboxConnection.objects.filter') as query,patch('project_management.services.transmittal_send.validate_mailbox_for_send'),patch('project_management.services.transmittal_send._resolve_mail_sender_user',return_value=self.owner),patch('userapis.mailbox_send.get_mail_access_token_for_sending',return_value='isolated-test-token'),patch('userapis.mailbox_send.requests.post') as post:
            query.return_value.first.return_value=mailbox
            post.return_value.status_code=202
            event=send_transmittal(self.transmittal,self.owner,mailbox_id=999)
        self.assertEqual(event.event_type,'SEND_SUCCEEDED',event.metadata)
        post.assert_called_once()
        message=post.call_args.kwargs['json']['message']
        self.assertEqual(message['toRecipients'][0]['emailAddress']['address'],self.client_contact.email)
        self.assertEqual(message['ccRecipients'][0]['emailAddress']['address'],self.second.email)
        attachment=message['attachments'][0]
        sent=base64.b64decode(attachment['contentBytes'])
        evidence=self.transmittal.events.get(event_type='SEND_STARTED').metadata['attachments'][0]
        self.assertEqual(hashlib.sha256(sent).hexdigest(),evidence['sha256'])
        self.assertEqual(attachment['name'],evidence['name'])

    def test_graph_attachment_size_limit_is_checked_before_http(self):
        from types import SimpleNamespace
        from userapis.models import MailboxConnection
        mailbox=SimpleNamespace(email_address='sender@links.invalid',connection_type=MailboxConnection.ConnectionType.SHARED)
        with patch('project_management.services.transmittal_send.MailboxConnection.objects.filter') as query,patch('project_management.services.transmittal_send.validate_mailbox_for_send'),patch('project_management.services.transmittal_send._resolve_mail_sender_user',return_value=self.owner),patch('userapis.mailbox_send.get_mail_access_token_for_sending',return_value='isolated-test-token'),patch('userapis.mailbox_send.MAX_GRAPH_ATTACHMENTS_BYTES',0),patch('userapis.mailbox_send.requests.post') as post:
            query.return_value.first.return_value=mailbox
            event=send_transmittal(self.transmittal,self.owner,mailbox_id=999)
        self.assertEqual(event.event_type,'SEND_FAILED')
        self.assertIn('too large',event.metadata['error'])
        self.assertFalse(event.metadata['uncertain'])
        post.assert_not_called()

    def test_graph_preparation_failure_can_be_retried_on_same_issued_transmittal(self):
        from types import SimpleNamespace
        from userapis.models import MailboxConnection
        mailbox=SimpleNamespace(email_address='sender@links.invalid',connection_type=MailboxConnection.ConnectionType.SHARED)
        with patch('project_management.services.transmittal_send.MailboxConnection.objects.filter') as query,patch('project_management.services.transmittal_send.validate_mailbox_for_send'),patch('project_management.services.transmittal_send._resolve_mail_sender_user',return_value=self.owner),patch('userapis.mailbox_send.get_mail_access_token_for_sending',return_value='isolated-test-token'),patch('userapis.mailbox_send.requests.post') as post:
            query.return_value.first.return_value=mailbox
            with patch('userapis.mailbox_send._graph_attachments',side_effect=AttributeError('missing file_size')):
                failed=send_transmittal(self.transmittal,self.owner,mailbox_id=999)
            self.assertEqual(failed.event_type,'SEND_FAILED')
            self.assertFalse(failed.metadata['uncertain'])
            post.assert_not_called()
            post.return_value.status_code=202
            result=send_transmittal(self.transmittal,self.owner,mailbox_id=999)
        self.assertEqual(result.event_type,'SEND_SUCCEEDED',result.metadata)
        self.assertEqual(self.transmittal.events.filter(event_type='SEND_STARTED').count(),2)
        self.assertEqual(self.transmittal.events.filter(event_type='SEND_SUCCEEDED').count(),1)
        post.assert_called_once()
