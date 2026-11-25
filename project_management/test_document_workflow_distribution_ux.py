from unittest.mock import patch
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import override_settings
from rest_framework.test import APIRequestFactory,force_authenticate
from project_management.models import DocumentWorkflow,Transmittal,TransmittalEvent
from project_management.test_transmittal import TransmittalTestBase,grant_transmittal_project_permissions
from project_management.services.document_revision import add_document_revision
from project_management.services.document_withdrawal import withdraw_document,assert_physically_deletable
from project_management.services.document_workflow import start_workflow,perform_action
from project_management.services.transmittal_items import add_transmittal_item
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_send import send_transmittal
from project_management.views.transmittal import TransmittalSendView

class WorkflowInteractionTests(TransmittalTestBase):
    def workflow(self, folder, kind='TWO_STEP'):
        doc=self.make_document(folder=folder)
        rev=self.add_revision(doc)
        return doc,rev,start_workflow(rev,kind,self.owner,self.approver if kind=='THREE_STEP' else None,self.approver,self.owner)

    def test_author_signs_and_approver_approves_without_comment(self):
        doc,rev,wf=self.workflow(self.company_folder)
        perform_action(wf,self.owner,'SIGN','')
        wf.refresh_from_db();self.assertEqual(wf.status,'APPROVE')
        perform_action(wf,self.approver,'APPROVE',None)
        wf.refresh_from_db();self.assertEqual(wf.status,'APPROVED')
        self.assertEqual(list(wf.events.order_by('id').values_list('comment',flat=True)),['','',''])
        self.add_revision(doc,content=b'new after approval')

    def test_three_step_optional_comments_and_required_rejection(self):
        doc,rev,wf=self.workflow(self.folder,'THREE_STEP')
        perform_action(wf,self.owner,'SIGN',None)
        perform_action(wf,self.approver,'VALIDATE','  ')
        wf.refresh_from_db();self.assertEqual(wf.status,'APPROVE')
        for comment in ['', ' ', None]:
            with self.assertRaises(ValidationError):perform_action(wf,self.approver,'REJECT',comment)
        perform_action(wf,self.approver,'REJECT','Please correct section 2')
        self.add_revision(doc,content=b'correction after rejection')

    def test_active_workflow_blocks_withdraw_delete_and_version_in_both_scopes(self):
        for folder in [self.company_folder,self.folder]:
            doc,rev,wf=self.workflow(folder,'THREE_STEP')
            for status,actor,action in [('DRAFT',self.owner,'SIGN'),('VERIFY',self.approver,'VALIDATE'),('APPROVE',None,None)]:
                wf.refresh_from_db();self.assertEqual(wf.status,status)
                for operation in [lambda:withdraw_document(doc,self.owner,'blocked'),lambda:assert_physically_deletable(doc),lambda:self.add_revision(doc,content=b'blocked')]:
                    with self.assertRaises(ValidationError):operation()
                if action:perform_action(wf,actor,action,'')
            doc.refresh_from_db();self.assertIsNone(doc.withdrawn_at)

@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',DEFAULT_FROM_EMAIL='links@test.invalid')
class TransmittalDistributionTests(TransmittalTestBase):
    def setUp(self):
        super().setUp()
        revision=self.make_approved_internal_revision()
        self.transmittal=self.create_transmittal(subject='Document distribution',message='Please find attached.')
        self.transmittal.external_contacts.add(self.client_contact)
        add_transmittal_item(self.transmittal,revision,'FOR_INFORMATION',self.owner)
        self.transmittal=finalize_transmittal(self.transmittal,self.owner)
        self.recipient=self.client_contact.pk

    def test_exact_frozen_file_is_sent_without_creating_correspondence(self):
        from project_management.models import Correspondence
        before=Correspondence.objects.count()
        event=send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.recipient)
        self.assertEqual(event.event_type,'SEND_SUCCEEDED')
        self.assertEqual(len(mail.outbox),1)
        self.assertEqual(mail.outbox[0].to,[self.client_contact.email])
        self.assertTrue(mail.outbox[0].attachments)
        self.assertEqual(Correspondence.objects.count(),before)
        from project_management.services.correspondances_register import annotate_ready_to_send
        row=annotate_ready_to_send([{'id':self.transmittal.pk,'entity_type':'TRANSMITTAL','direction':'OUT','lifecycle_status':'ISSUED'}])[0]
        self.assertEqual(row['delivery_status'],'SENT')
        start=self.transmittal.events.get(event_type='SEND_STARTED')
        self.assertTrue(start.metadata['attachments'][0]['sha256'])

    def test_double_submit_does_not_send_again(self):
        send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.recipient)
        with self.assertRaises(ValidationError):send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.recipient)
        self.assertEqual(len(mail.outbox),1)

    def test_withdrawn_document_cannot_be_sent_from_an_already_issued_transmittal(self):
        doc=self.transmittal.items.first().document_revision.document
        withdraw_document(doc,self.owner,'not for distribution')
        with self.assertRaises(ValidationError):send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.recipient)
        self.assertFalse(self.transmittal.events.filter(event_type='SEND_STARTED').exists())

    def test_foreign_recipient_is_rejected_before_an_attempt(self):
        with self.assertRaises(ValidationError):send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.orphan_contact.pk)
        self.assertFalse(self.transmittal.events.filter(event_type='SEND_STARTED').exists())

    def test_uncertain_transport_failure_cannot_be_repeated(self):
        with patch('project_management.services.transmittal_send._deliver_via_provider',side_effect=TimeoutError('connection lost')):
            event=send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.recipient)
        self.assertEqual(event.event_type,'SEND_FAILED');self.assertTrue(event.metadata['uncertain'])
        with self.assertRaises(ValidationError):send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.recipient)

    def test_mailbox_other_company_is_denied_before_an_attempt(self):
        with self.assertRaises(ValidationError):send_transmittal(self.transmittal,self.owner,recipient_contact_id=self.recipient,mailbox_id=999999999)
        self.assertFalse(self.transmittal.events.filter(event_type='SEND_STARTED').exists())

    @patch.object(TransmittalSendView, "permission_classes", [])
    def test_api_requires_issue_permission_not_just_update(self):
        # The fixture has no subscription. Only the global licensing gate is isolated;
        # the endpoint still executes its real object-level TransmittalPermission.
        grant_transmittal_project_permissions(self.owner,self.project,self.company,actions=('update',))
        request=APIRequestFactory().post('/send',{'recipient_contact':self.recipient},format='json');force_authenticate(request,self.owner)
        view=TransmittalSendView.as_view({'post':'create'})
        self.assertEqual(view(request,transmittal_id=self.transmittal.pk).status_code,403)
        grant_transmittal_project_permissions(self.owner,self.project,self.company,actions=('issue',))
        request=APIRequestFactory().post('/send',{'recipient_contact':self.recipient},format='json');force_authenticate(request,self.owner)
        self.assertEqual(view(request,transmittal_id=self.transmittal.pk).status_code,200)
